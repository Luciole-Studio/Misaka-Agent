"""pi-ai's ``typesafe-system-one.test.ts``, ``cloudflare-workers-ai-system-one.test.ts`` and
``llama-cpp-classify.test.ts`` (``misaka/ai/providers/``).

Upstream injects ``fetch``; misaka does not port that option (httpx sends every request), so
these tests stand in for it by patching ``httpx.AsyncClient.post``.
"""

from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urlsplit

import httpx
import pytest

from misaka.ai.api import classifier_shared, llama_cpp_classify
from misaka.ai.api.llama_cpp_classify import (
    answerFromProbabilities,
    labelProbabilities,
    llamaServerRoot,
    peakConfidence,
    renderQuestion,
)
from misaka.ai.api.typesafe_system_one import classify as typesafeClassify
from misaka.ai.models import createModels
from misaka.ai.providers.all import getBuiltinClassifierModel
from misaka.ai.providers.cloudflare_workers_ai import cloudflareWorkersAIProvider
from misaka.ai.types import (
    ClassifierContext,
    ClassifierModel,
    ClassifierScoreQuestion,
    ImageContent,
)
from misaka.utils.js import AbortController


def _fetch(monkeypatch, handler) -> list[dict]:
    """``options.fetch``: every POST goes to ``handler(url, body, headers)``."""
    requests: list[dict] = []

    async def post(self, url, *, headers=None, content=None, **_kwargs):
        body = json.loads(content)
        request = {"url": str(url), "body": body, "headers": {name.lower(): value for name, value in (headers or {}).items()}}
        requests.append(request)
        response = await handler(str(url), body, request["headers"])
        response.request = httpx.Request("POST", url)
        return response

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    return requests


def _json(value, status: int = 200, headers=None) -> httpx.Response:
    return httpx.Response(status, json=value, headers=headers)


# --- typesafe-system-one.test.ts --------------------------------------------------------------

MODEL = ClassifierModel.model_validate({
    "type": "classifier", "id": "jev-latest", "name": "Jev", "api": "typesafe-system-one", "provider": "typesafe",
    "baseUrl": "https://api.typesafe.ai/v1/", "input": ["text"],
    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}, "contextWindow": 64000,
})

CONTEXT = ClassifierContext.model_validate({
    "state": {"text": "The deployment succeeded, thank you."},
    "questions": {
        "category": {"type": "choice", "instructions": "Classify the message",
                     "criteria": {"success": "Successful", "failure": "Failed"}},
        "satisfaction": {"type": "score", "instructions": "Score satisfaction", "criteria": ["low", "neutral", "high"]},
        "approved": {"type": "bool", "instructions": "Does the user approve?",
                     "criteria": {"true": "Approval", "false": "No approval"}},
    },
})

WIRE_ANSWERS = {
    "category": {"type": "choice", "choice": "success", "probabilities": {"success": 0.9, "failure": 0.1}, "confidence": 0.8},
    "satisfaction": {"type": "score", "score": 2, "confidence": 0.7},
    "approved": {"type": "noul", "noul": 0.95},
}


async def test_typesafe_maps_public_bool_questions_and_answers_to_noul_values(monkeypatch):
    async def reply(url, body, headers):
        assert body["model"] == "jev-latest"
        assert [body["questions"][id]["type"] for id in ("category", "satisfaction", "approved")] == ["choice", "score", "noul"]
        # System One has no temperature field; the option is ignored.
        assert "temperature" not in body
        assert headers["authorization"] == "Bearer secret"
        return _json({"answers": WIRE_ANSWERS})

    requests = _fetch(monkeypatch, reply)
    result = await typesafeClassify(MODEL, CONTEXT, {"apiKey": "secret", "temperature": 1.5})

    async def priced(url, body, headers):
        return _json({"answers": WIRE_ANSWERS, "usage": {"input_tokens": 308, "output_tokens": 23}})

    _fetch(monkeypatch, priced)
    pricedResult = await typesafeClassify(
        MODEL.model_copy(update={"cost": MODEL.cost.model_copy(update={"input": 0.042})}), CONTEXT, {"apiKey": "secret"}
    )

    assert [request["url"] for request in requests] == ["https://api.typesafe.ai/v1/systemone"]
    assert result.stopReason == "stop"
    assert result.answers["approved"].model_dump() == {"type": "bool", "probability": 0.95}
    assert (result.answers["category"].type, result.answers["category"].choice) == ("choice", "success")
    assert result.answers["satisfaction"].model_dump() == {"type": "score", "score": 2, "confidence": 0.7}
    assert result.usage is None
    assert (pricedResult.usage.input, pricedResult.usage.output, pricedResult.usage.totalTokens) == (308, 23, 331)
    assert pricedResult.usage.cost.total == pytest.approx(0.000012936, abs=1e-12)


async def test_typesafe_posts_openrouter_requests_to_its_compatible_endpoint(monkeypatch):
    async def reply(url, body, headers):
        assert (body["model"], body["state"]) == ("typesafe/jev-1.13", CONTEXT.state)
        # Response shape observed from the live OpenRouter endpoint.
        return _json({"id": "gen-dec-1", "provider": "TypeSafe", "answers": WIRE_ANSWERS,
                      "usage": {"input_tokens": 308, "output_tokens": 23, "cost": 0.000012936}})

    requests = _fetch(monkeypatch, reply)
    openRouterModel = MODEL.model_copy(update={
        "id": "typesafe/jev-1.13", "provider": "openrouter", "baseUrl": "https://openrouter.ai/api/v1",
        "cost": MODEL.cost.model_copy(update={"input": 0.042}),
    })

    result = await typesafeClassify(openRouterModel, CONTEXT, {"apiKey": "secret"})

    assert requests[0]["url"] == "https://openrouter.ai/api/v1/systemone"
    assert result.stopReason == "stop"
    assert result.answers["approved"].model_dump() == {"type": "bool", "probability": 0.95}
    # Priced from the catalog like chat usage; matches OpenRouter's reported cost.
    assert result.usage.cost.total == pytest.approx(0.000012936, abs=1e-12)


async def test_typesafe_rejects_models_for_other_classifier_apis(monkeypatch):
    async def reply(*_):
        return _json({"answers": WIRE_ANSWERS})

    requests = _fetch(monkeypatch, reply)
    result = await typesafeClassify(
        MODEL.model_copy(update={"api": "cloudflare-workers-ai-system-one"}), CONTEXT, {"apiKey": "secret"}
    )

    assert requests == []
    assert result.stopReason == "error"
    assert "Unsupported classifier API: cloudflare-workers-ai-system-one" in result.errorMessage


async def test_typesafe_rejects_image_input_before_sending(monkeypatch):
    async def reply(*_):
        return _json({"answers": WIRE_ANSWERS})

    requests = _fetch(monkeypatch, reply)
    result = await typesafeClassify(
        MODEL.model_copy(update={"input": ["text", "image"]}),
        CONTEXT.model_copy(update={"images": [ImageContent(type="image", data="aW1hZ2U=", mimeType="image/png")]}),
        {"apiKey": "secret"},
    )

    assert requests == []
    assert result.stopReason == "error"
    assert result.errorMessage == "System One API does not support image input"


async def test_typesafe_merges_headers_case_insensitively_and_supports_null_suppression(monkeypatch):
    async def reply(*_):
        return _json({"answers": WIRE_ANSWERS})

    requests = _fetch(monkeypatch, reply)
    modelWithHeaders = MODEL.model_copy(update={"headers": {"authorization": "Bearer model", "X-Source": "model"}})

    await typesafeClassify(modelWithHeaders, CONTEXT, {
        "apiKey": "secret", "headers": {"Authorization": "Bearer request", "x-source": "request"},
    })
    await typesafeClassify(modelWithHeaders, CONTEXT, {"apiKey": "secret", "headers": {"Authorization": None}})

    assert (requests[0]["headers"]["authorization"], requests[0]["headers"]["x-source"]) == ("Bearer request", "request")
    assert "authorization" not in requests[1]["headers"]


async def test_typesafe_preserves_prototype_sensitive_question_ids(monkeypatch):
    async def reply(*_):
        return _json({"answers": {"__proto__": {"type": "noul", "noul": 0.75}}})

    _fetch(monkeypatch, reply)
    context = ClassifierContext.model_validate({"state": {}, "questions": {
        "__proto__": {"type": "bool", "instructions": "Is this true?", "criteria": {"true": "Yes", "false": "No"}},
    }})

    result = await typesafeClassify(MODEL, context, {"apiKey": "secret"})

    assert result.stopReason == "stop"
    assert list(result.answers) == ["__proto__"]
    assert result.answers["__proto__"].model_dump() == {"type": "bool", "probability": 0.75}


async def test_typesafe_reports_request_timeouts_separately_from_caller_cancellation(monkeypatch):
    async def hang(*_):
        await asyncio.Event().wait()

    _fetch(monkeypatch, hang)
    result = await typesafeClassify(MODEL, CONTEXT, {"apiKey": "secret", "timeoutMs": 5, "maxRetries": 0})

    assert result.stopReason == "error"
    assert result.errorMessage == "Request timed out after 5ms"


async def test_typesafe_creates_a_fresh_timeout_for_every_retry_attempt(monkeypatch):
    signals: list = []

    class Recorded(classifier_shared.TimeoutAbortSignal):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            signals.append(self)

    monkeypatch.setattr(classifier_shared, "TimeoutAbortSignal", Recorded)
    attempts = 0

    async def reply(*_):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(500, text="retry", headers={"retry-after-ms": "0"})
        return _json({"answers": WIRE_ANSWERS})

    _fetch(monkeypatch, reply)
    result = await typesafeClassify(MODEL, CONTEXT, {"apiKey": "secret", "timeoutMs": 1000, "maxRetries": 1})

    assert result.stopReason == "stop"
    assert len(signals) == 2 and signals[0] is not signals[1]


async def test_typesafe_returns_malformed_responses_as_classifier_errors(monkeypatch):
    async def reply(*_):
        return _json({"answers": {}, "usage": {"input_tokens": 10, "output_tokens": 2}})

    _fetch(monkeypatch, reply)
    result = await typesafeClassify(MODEL, CONTEXT, {"apiKey": "secret"})

    assert result.stopReason == "error"
    assert result.answers == {}
    assert "did not return an answer for category" in result.errorMessage
    # The request was billed, so its usage is kept.
    assert (result.usage.input, result.usage.output) == (10, 2)


async def test_typesafe_ignores_malformed_usage(monkeypatch):
    async def many(*_):
        return _json({"answers": WIRE_ANSWERS, "usage": {"input_tokens": "many", "output_tokens": 3}})

    _fetch(monkeypatch, many)
    result = await typesafeClassify(MODEL, CONTEXT, {"apiKey": "secret"})

    async def costOnly(*_):
        return _json({"answers": WIRE_ANSWERS, "usage": {"cost": 0.1}})

    _fetch(monkeypatch, costOnly)
    withoutTokens = await typesafeClassify(MODEL, CONTEXT, {"apiKey": "secret"})

    assert result.stopReason == "stop"
    assert (result.usage.input, result.usage.output, result.usage.totalTokens) == (0, 3, 3)
    assert withoutTokens.stopReason == "stop" and withoutTokens.usage is None


# --- cloudflare-workers-ai-system-one.test.ts -------------------------------------------------

CF_CONTEXT = ClassifierContext.model_validate({
    "state": {"message": "Help! My payouts have been failing for 3 days."},
    "questions": {
        "is_urgent": {"type": "bool", "instructions": "Does this convey urgency?",
                      "criteria": {"true": "Explicitly time-sensitive", "false": "No urgency expressed"}},
        "department": {"type": "choice", "instructions": "Which team should handle this?",
                       "criteria": {"billing": "Payments", "technical": "Bugs"}},
    },
})

# Model output from https://developers.cloudflare.com/ai/models/typesafe/jev/
JEV_OUTPUT = {
    "model": "jev-1.13.0",
    "answers": {
        "is_urgent": {"type": "noul", "noul": 0.95},
        "department": {"type": "choice", "choice": "billing", "confidence": 0.8,
                       "probabilities": {"billing": 0.87, "technical": 0.13}},
    },
    "usage": {"input_tokens": 426, "output_tokens": 73},
}


def _rest(state: str, result=JEV_OUTPUT) -> dict:
    """REST envelope observed from the live /ai/run endpoint."""
    return {"result": {"state": state, "result": result, "gatewayMetadata": {"keySource": "Unified"}},
            "success": True, "errors": [], "messages": []}


def _cloudflare():
    models = createModels()
    models.setProvider(cloudflareWorkersAIProvider())
    jev = models.getModelOfType("classifier", "cloudflare-workers-ai", "typesafe/jev")
    assert jev is not None
    return models, jev


CF_AUTH = {"apiKey": "cf-key", "env": {"CLOUDFLARE_ACCOUNT_ID": "account-id"}}


def test_cloudflare_exposes_jev_only_through_classifier_catalog_accessors():
    models, jev = _cloudflare()
    assert jev == getBuiltinClassifierModel("cloudflare-workers-ai", "typesafe/jev")
    assert (jev.type, jev.api) == ("classifier", "cloudflare-workers-ai-system-one")
    assert models.getModel("cloudflare-workers-ai", "typesafe/jev") is None


async def test_cloudflare_runs_jev_through_the_account_scoped_run_endpoint(monkeypatch):
    models, jev = _cloudflare()

    async def reply(url, body, headers):
        assert body["model"] == "typesafe/jev"
        assert body["input"]["state"] == CF_CONTEXT.state
        assert (body["input"]["questions"]["is_urgent"]["type"], body["input"]["questions"]["department"]["type"]) == (
            "noul", "choice")
        assert headers["authorization"] == "Bearer cf-key"
        return _json(_rest("Completed"))

    requests = _fetch(monkeypatch, reply)
    result = await models.classify(jev, CF_CONTEXT, CF_AUTH)

    assert requests[0]["url"] == "https://api.cloudflare.com/client/v4/accounts/account-id/ai/run"
    assert result.stopReason == "stop", result.errorMessage
    assert result.answers["is_urgent"].model_dump() == {"type": "bool", "probability": 0.95}
    department = result.answers["department"]
    assert (department.type, department.choice, department.confidence) == ("choice", "billing", 0.8)
    assert (result.usage.input, result.usage.output, result.usage.totalTokens) == (426, 73, 499)


# Cloudflare-hosted output observed from a live /ai/run call, question ids renamed to match `context`.
# The envelope carries the output directly, without a run record.
CLEF_OUTPUT = {
    "model": "clef",
    "answers": {
        "is_urgent": {"type": "noul", "noul": 0.9912},
        "department": {
            "type": "choice",
            "choice": "technical",
            "probabilities": {"billing": 0.1632, "technical": 0.8368},
            "confidence": 0.4538,
        },
    },
    "usage": {"input_tokens": 222, "output_tokens": 0},
}


@pytest.mark.parametrize(("id", "inputPrice"), [("@cf/cloudflare/clef", 0.24), ("@cf/cloudflare/clef-flash", 0.09)])
async def test_cloudflare_runs_clef_through_the_run_endpoint_and_parses_its_direct_output(monkeypatch, id, inputPrice):
    models, _jev = _cloudflare()
    clef = models.getModelOfType("classifier", "cloudflare-workers-ai", id)
    assert clef is not None, f"missing Cloudflare {id} model"

    async def reply(url, body, headers):
        assert body["model"] == id
        assert body["input"]["state"] == CF_CONTEXT.state
        assert body["input"]["questions"]["is_urgent"]["type"] == "noul"
        return _json({"result": CLEF_OUTPUT, "success": True, "errors": [], "messages": []})

    requests = _fetch(monkeypatch, reply)
    result = await models.classify(clef, CF_CONTEXT, CF_AUTH)

    assert requests[0]["url"] == "https://api.cloudflare.com/client/v4/accounts/account-id/ai/run"
    assert result.stopReason == "stop", result.errorMessage
    assert result.answers["is_urgent"].model_dump() == {"type": "bool", "probability": 0.9912}
    department = result.answers["department"]
    assert (department.type, department.choice, department.confidence) == ("choice", "technical", 0.4538)
    assert (result.usage.input, result.usage.output, result.usage.totalTokens) == (222, 0, 222)
    assert result.usage.cost.input == pytest.approx((222 * inputPrice) / 1_000_000)


async def test_cloudflare_reports_runs_that_did_not_complete(monkeypatch):
    models, jev = _cloudflare()

    async def reply(*_):
        return _json(_rest("Queued", None))

    _fetch(monkeypatch, reply)
    result = await models.classify(jev, CF_CONTEXT, CF_AUTH)

    assert result.stopReason == "error"
    assert "run did not complete (state: Queued)" in result.errorMessage


async def test_cloudflare_reports_envelope_errors(monkeypatch):
    models, jev = _cloudflare()

    async def reply(*_):
        return _json({"success": False, "errors": [{"code": 5007, "message": "No such model"}], "result": None})

    _fetch(monkeypatch, reply)
    result = await models.classify(jev, CF_CONTEXT, CF_AUTH)

    assert result.stopReason == "error"
    assert "Cloudflare Workers AI error: No such model" in result.errorMessage


# --- llama-cpp-classify.test.ts ---------------------------------------------------------------

_serverCount = 0


def _llamaModel() -> ClassifierModel:
    """A fresh server URL per test: label tokens are cached per server and model."""
    global _serverCount
    _serverCount += 1
    return ClassifierModel.model_validate({
        "type": "classifier", "id": "qwen", "name": "qwen", "api": "llama-cpp-classify", "provider": "llama.cpp",
        "baseUrl": f"http://llama-{_serverCount}.test:8080/v1", "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}, "contextWindow": 32768,
    })


def charTokens(content: str) -> list[int]:
    """Token IDs: one per character, the character code."""
    return [ord(char) for char in content]


def _fakeServer(monkeypatch, next=None, template=None, tokenize=None) -> list[dict]:
    async def reply(url, body, headers):
        path = urlsplit(url).path
        if path == "/tokenize":
            return _json({"tokens": (tokenize or charTokens)(str(body["content"]))})
        if path == "/apply-template":
            messages = body["messages"]
            prompt = (
                template(messages)
                if template is not None
                else "".join(f"<|{message['role']}|>\n{message['content']}\n" for message in messages) + "<|assistant|>\n"
            )
            return _json({"prompt": prompt})
        if path == "/completion":
            logprobs = next(str(body["prompt"]), int(body["n_probs"])) if next is not None else {"A": -0.1, "B": -2.5}
            top = [{"id": ord(token[0]), "token": token, "bytes": [], "logprob": logprob} for token, logprob in logprobs.items()]
            return _json({"content": "A", "completion_probabilities": [{"id": 65, "token": "A", "top_logprobs": top}]})
        return httpx.Response(404, text="not found")

    return _fetch(monkeypatch, reply)


def answerByPrompt(prompt: str, _depth: int) -> dict[str, float]:
    """Completion log-probabilities for bool (Yes/No) and letter labels; the fake tokenizer maps a
    label to its first character."""
    if "Answer Yes or No." in prompt:
        return {"Y": -0.05, "N": -3}
    if "Answer with one level number." in prompt:
        return {"2": -0.2, "1": -1.8, "0": -4}
    return {"B": -0.3, "A": -1.5, "C": -3}


LLAMA_CONTEXT = ClassifierContext.model_validate({
    "state": {"message": "Help! My payouts have been failing for 3 days."},
    "questions": {
        "team": {"type": "choice", "instructions": "Which team should handle this?",
                 "criteria": {"billing": "Payments and refunds", "technical": "Bugs and outages", "sales": ""}},
        "urgent": {"type": "bool", "instructions": "Does this convey urgency?",
                   "criteria": {"true": "The user needs help soon", "false": "No time pressure"}},
        "severity": {"type": "score", "instructions": "How severe is this?", "criteria": ["low", "medium", "high"]},
    },
})


def wordTokens(content: str) -> list[int]:
    """Maps multi-character labels to single tokens, as a real vocabulary would."""
    words = {"Yes": 89, "No": 78}
    tokens: list[int] = []
    for part in re.split(r"(\n)", content):
        if part == "":
            continue
        tokens.extend([words[part]] if part in words else charTokens(part))
    return tokens


def _pick(criteria=None) -> ClassifierContext:
    return ClassifierContext.model_validate({"state": {}, "questions": {
        "pick": {"type": "choice", "instructions": "Pick one", "criteria": criteria or {"a": "", "b": ""}},
    }})


async def test_llama_answers_choice_bool_and_score_questions_from_label_log_probabilities(monkeypatch):
    requests = _fakeServer(monkeypatch, next=answerByPrompt, tokenize=wordTokens)
    model = _llamaModel()

    result = await llama_cpp_classify.classify(model, LLAMA_CONTEXT, {"apiKey": "local"})

    assert result.errorMessage is None
    assert result.stopReason == "stop"
    choice = labelProbabilities([-1.5, -0.3, -3], 1)
    assert result.answers["team"].model_dump() == {
        "type": "choice", "choice": "technical",
        "probabilities": {"billing": choice[0], "technical": choice[1], "sales": choice[2]},
        "confidence": peakConfidence(choice),
    }
    assert result.answers["urgent"].model_dump() == {"type": "bool", "probability": labelProbabilities([-0.05, -3], 1)[0]}
    levels = labelProbabilities([-4, -1.8, -0.2], 1)
    assert result.answers["severity"].model_dump() == {
        "type": "score", "score": levels[1] + 2 * levels[2], "confidence": peakConfidence(levels),
    }

    root = model.baseUrl.removesuffix("/v1")
    for request in requests:
        assert request["url"].startswith(f"{root}/")
        assert request["body"]["model"] == "qwen"
        assert request["headers"]["authorization"] == "Bearer local"
    completion = next(request for request in requests if request["url"].endswith("/completion"))
    assert {key: completion["body"][key] for key in ("n_predict", "n_probs", "post_sampling_probs", "cache_prompt")} == {
        "n_predict": 1, "n_probs": 256, "post_sampling_probs": False, "cache_prompt": True,
    }
    template = next(request for request in requests if request["url"].endswith("/apply-template"))
    assert template["body"]["chat_template_kwargs"] == {"enable_thinking": False}


def test_llama_repeats_the_state_around_all_questions_and_ends_with_this_questions_labels():
    rendered = renderQuestion(LLAMA_CONTEXT, "team")
    state = 'State:\n{\n "message": "Help! My payouts have been failing for 3 days."\n}'
    assert rendered.labels == ["A", "B", "C"]
    assert rendered.keys == ["billing", "technical", "sales"]
    assert rendered.content == "\n".join([  # noqa: FLY002 - upstream lists the lines
        state, "",
        "Task: answer each of the following questions about the state.", "",
        "Question: Which team should handle this?", "",
        "Options:", "- billing: Payments and refunds", "- technical: Bugs and outages", "- sales", "",
        "Question: Does this convey urgency?", "",
        "Yes means: The user needs help soon", "No means: No time pressure", "",
        "Question: How severe is this?", "",
        "Levels:", "0. low", "1. medium", "2. high", "",
        state, "",
        "Question: Which team should handle this?", "",
        "Options:", "A. billing: Payments and refunds", "B. technical: Bugs and outages", "C. sales", "",
        "Answer with one letter.",
    ])


def test_llama_shares_everything_before_the_final_question_across_the_questions_of_a_request():
    def prefix(id: str) -> str:
        content = renderQuestion(LLAMA_CONTEXT, id).content
        return content[: content.rindex("Question:")]

    assert prefix("urgent") == prefix("team")
    assert prefix("severity") == prefix("team")
    assert renderQuestion(LLAMA_CONTEXT, "urgent").content.endswith("No means: No time pressure\n\nAnswer Yes or No.")
    assert renderQuestion(LLAMA_CONTEXT, "severity").content.endswith("2. high\n\nAnswer with one level number.")


async def test_llama_divides_label_log_probabilities_by_the_temperature(monkeypatch):
    _fakeServer(monkeypatch, next=lambda *_: {"A": -0.1, "B": -2.5})

    result = await llama_cpp_classify.classify(_llamaModel(), _pick(), {"temperature": 2})

    expected = labelProbabilities([-0.1 / 2, -2.5 / 2], 1)
    assert result.answers["pick"].probabilities == {"a": expected[0], "b": expected[1]}
    assert labelProbabilities([-0.1, -2.5], 2) == expected


async def test_llama_rejects_non_positive_temperatures_before_sending_requests(monkeypatch):
    requests = _fakeServer(monkeypatch)
    result = await llama_cpp_classify.classify(_llamaModel(), LLAMA_CONTEXT, {"temperature": 0})

    assert result.stopReason == "error"
    assert "Temperature must be a positive number, got 0" in result.errorMessage
    assert requests == []


def _depths(requests) -> list[int]:
    return [int(request["body"]["n_probs"]) for request in requests if request["url"].endswith("/completion")]


async def test_llama_retries_deeper_readouts_when_a_label_is_missing_and_fails_without_inventing_zeros(monkeypatch):
    deep = _fakeServer(monkeypatch, next=lambda _prompt, depth: {"A": -0.1} if depth < 4096 else {"A": -0.1, "B": -9})
    recovered = await llama_cpp_classify.classify(_llamaModel(), _pick(), {})
    assert recovered.stopReason == "stop"
    assert _depths(deep) == [256, 4096]

    never = _fakeServer(monkeypatch, next=lambda *_: {"A": -0.1})
    failed = await llama_cpp_classify.classify(_llamaModel(), _pick(), {})
    assert failed.stopReason == "error"
    assert failed.answers == {}
    assert "did not rank labels B for pick within the top 32768 tokens" in failed.errorMessage
    assert _depths(never) == [256, 4096, 32768]


async def test_llama_closes_a_reasoning_block_the_template_leaves_open(monkeypatch):
    requests = _fakeServer(monkeypatch, template=lambda _messages: "<|assistant|>\n<think>")
    await llama_cpp_classify.classify(_llamaModel(), _pick(), {})

    completion = next(request for request in requests if request["url"].endswith("/completion"))
    assert completion["body"]["prompt"] == "<|assistant|>\n<think></think>"


async def test_llama_reads_labels_in_reply_position_and_rejects_labels_that_are_not_one_token(monkeypatch):
    # A tokenizer that merges a newline with a following letter falls back to the label alone.
    _fakeServer(monkeypatch, tokenize=lambda content: [1000] if content.startswith("\n") and len(content) > 1 else charTokens(content))
    merged = await llama_cpp_classify.classify(_llamaModel(), _pick(), {})
    assert merged.stopReason == "stop"

    # The default fake tokenizer splits "Yes" into three tokens.
    _fakeServer(monkeypatch)
    result = await llama_cpp_classify.classify(_llamaModel(), ClassifierContext.model_validate({"state": {}, "questions": {
        "ok": {"type": "bool", "instructions": "OK?", "criteria": {"true": "", "false": ""}},
    }}), {})
    assert result.stopReason == "error"
    assert 'Label "Yes" is not a single token for qwen' in result.errorMessage


async def test_llama_caches_label_tokens_per_server_and_model(monkeypatch):
    model = _llamaModel()
    requests = _fakeServer(monkeypatch)
    await llama_cpp_classify.classify(model, _pick(), {})
    firstTokenizations = len([request for request in requests if request["url"].endswith("/tokenize")])
    await llama_cpp_classify.classify(model, _pick(), {})

    assert firstTokenizations > 0
    assert len([request for request in requests if request["url"].endswith("/tokenize")]) == firstTokenizations


async def test_llama_validates_option_counts_before_sending_requests(monkeypatch):
    requests = _fakeServer(monkeypatch)
    tooMany = await llama_cpp_classify.classify(
        _llamaModel(), _pick({f"option{index}": "" for index in range(63)}), {}
    )
    tooFew = await llama_cpp_classify.classify(_llamaModel(), ClassifierContext.model_validate({"state": {}, "questions": {
        "rate": {"type": "score", "instructions": "Rate", "criteria": ["only"]},
    }}), {})

    assert "A choice question needs 2 to 62 options, got 63" in tooMany.errorMessage
    assert "A score question needs 2 to 10 levels, got 1" in tooFew.errorMessage
    assert requests == []


async def test_llama_passes_completion_payloads_and_responses_through_the_request_hooks(monkeypatch):
    requests = _fakeServer(monkeypatch)
    payloads: list = []
    statuses: list = []

    def onPayload(payload, _model):
        payloads.append(payload)
        return {**payload, "id_slot": 1}

    await llama_cpp_classify.classify(_llamaModel(), _pick(), {
        "onPayload": onPayload, "onResponse": lambda response, _model: statuses.append(response["status"]),
    })

    assert len(payloads) == 1 and payloads[0]["n_predict"] == 1
    assert statuses == [200]
    assert next(request for request in requests if request["url"].endswith("/completion"))["body"]["id_slot"] == 1


async def test_llama_reports_server_errors_and_cancellation(monkeypatch):
    async def failing(*_):
        return httpx.Response(400, text='{"error":{"message":"context overflow"}}')

    _fetch(monkeypatch, failing)
    result = await llama_cpp_classify.classify(_llamaModel(), LLAMA_CONTEXT, {"maxRetries": 0})
    assert result.stopReason == "error"
    assert "llama.cpp error (400)" in result.errorMessage and "context overflow" in result.errorMessage

    async def empty(*_):
        return _json({})

    _fetch(monkeypatch, empty)
    controller = AbortController()
    controller.abort()
    aborted = await llama_cpp_classify.classify(_llamaModel(), LLAMA_CONTEXT, {"signal": controller.signal})
    assert aborted.stopReason == "aborted"


async def test_llama_rejects_models_for_other_classifier_apis(monkeypatch):
    requests = _fakeServer(monkeypatch)
    result = await llama_cpp_classify.classify(
        _llamaModel().model_copy(update={"api": "typesafe-system-one"}), LLAMA_CONTEXT, {}
    )

    assert "Unsupported classifier API: typesafe-system-one" in result.errorMessage
    assert requests == []


def test_llama_derives_the_server_root_from_openai_compatible_base_urls():
    assert llamaServerRoot("http://127.0.0.1:8080/v1/") == "http://127.0.0.1:8080"
    assert llamaServerRoot("https://example.com/prefix/v1") == "https://example.com/prefix"
    assert llamaServerRoot("http://127.0.0.1:8080") == "http://127.0.0.1:8080"


def test_llama_computes_typesafes_confidence_and_expected_scores():
    assert peakConfidence([0.89, 0.06, 0.05]) == pytest.approx(0.835)
    assert peakConfidence([0.5, 0.5]) == 0
    assert peakConfidence([1, 0, 0]) == 1
    answer = answerFromProbabilities(
        ClassifierScoreQuestion(instructions="", criteria=["a", "b", "c"]), ["0", "1", "2"], [0.2, 0.3, 0.5]
    )
    assert answer.model_dump() == {"type": "score", "score": 1.3, "confidence": peakConfidence([0.2, 0.3, 0.5])}


# --- MISAKA regressions (B4 review) ---------------------------------------------------------


async def test_a_failed_connection_is_not_retried_as_fetch_does_not_retry_it(monkeypatch):
    attempts = []

    async def post(self, url, **_kwargs):
        attempts.append(url)
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    result = await typesafeClassify(MODEL, CONTEXT, {"apiKey": "secret", "maxRetryDelayMs": 1})

    assert len(attempts) == 1
    assert result.stopReason == "error"
    assert "fetch failed" in result.errorMessage


def test_the_rendered_state_spells_numbers_as_json_stringify_does():
    rendered = llama_cpp_classify._renderState({"count": 3.0, "ratio": 0.5, "missing": float("nan"), "flag": True})
    assert rendered == 'State:\n{\n "count": 3,\n "ratio": 0.5,\n "missing": null,\n "flag": true\n}'

"""Translated from pi's ``packages/ai/src/api/openai-decisions.ts`` (v1.1.0).

OpenAI's Decisions API: `POST /v1/decisions` with `{ model, input, questions }`.
https://developers.openai.com/api/docs/guides/decisions

The state is sent as JSON text. With images, the input becomes one user message with the state
as `input_text` followed by `input_image` data URLs. Questions map to Decisions types:
`choice` to `choice`, `score` to `score`, and `bool` to `predicate`. Predicates have no
criteria field, so the meanings of true and false are appended to the instructions.

Only OpenAI API keys work: Sign in with ChatGPT tokens are rejected on this route.

MISAKA fork (each is marked where it happens): a plain mapping context is read into ``ClassifierContext``, the request
URL is a string.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, Literal, NotRequired, TypedDict
from urllib.parse import urljoin

from misaka.ai.api._common import _option
from misaka.ai.api.classifier_shared import (
    isRecord,
    parseClassifierUsage,
    postClassifierRequest,
    requiredNumber,
)
from misaka.ai.types import (
    ClassifierAnswerValue,
    ClassifierBoolAnswer,
    ClassifierChoiceAnswer,
    ClassifierContext,
    ClassifierModel,
    ClassifierQuestionValue,
    ClassifierResult,
    ClassifierScoreAnswer,
)
from misaka.ai.utils.error_body import formatProviderError, normalizeProviderError
from misaka.utils.js import jsonStringify, objectEntries
from misaka.utils.values import signal_aborted

_LABEL = "OpenAI Decisions"

# The endpoint accepts at most this many image parts per request.
_MAX_IMAGES = 128


class _DecisionQuestion(TypedDict):
    type: Literal["predicate", "choice", "score"]
    name: str
    instructions: str
    choices: NotRequired[list[dict[str, str]]]
    levels: NotRequired[list[dict[str, str]]]


def _predicateInstructions(question: Any) -> str:
    meanings = [
        meaning
        for meaning in (
            f"True means: {question.criteria.true}" if question.criteria.true else "",
            f"False means: {question.criteria.false}" if question.criteria.false else "",
        )
        if meaning
    ]
    return f"{question.instructions}\n\n" + "\n".join(meanings) if len(meanings) > 0 else question.instructions


def _wireQuestion(name: str, question: ClassifierQuestionValue) -> _DecisionQuestion:
    if question.type == "choice":
        return {
            "type": "choice",
            "name": name,
            "instructions": question.instructions,
            "choices": [
                {"value": value, "description": description} if description else {"value": value}
                for value, description in objectEntries(question.criteria)
            ],
        }
    if question.type == "score":
        return {
            "type": "score",
            "name": name,
            "instructions": question.instructions,
            "levels": [{"label": label} for label in question.criteria],
        }
    return {"type": "predicate", "name": name, "instructions": _predicateInstructions(question)}


def _wireInput(context: ClassifierContext) -> Any:
    state = jsonStringify(context.state)
    images = context.images if context.images is not None else []
    if len(images) == 0:
        return state
    if len(images) > _MAX_IMAGES:
        raise RuntimeError(f"{_LABEL} accepts at most {_MAX_IMAGES} images, got {len(images)}")
    return [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": state},
                *(
                    {
                        "type": "input_image",
                        "image_url": f"data:{image.mimeType};base64,{image.data}",
                    }
                    for image in images
                ),
            ],
        },
    ]


def _choiceProbabilities(value: Any, id: str) -> dict[str, float]:
    if not isinstance(value, list):
        raise RuntimeError(f"{_LABEL} returned invalid probabilities for {id}")  # noqa: TRY004 - upstream throws Error

    def entryOf(entry: Any) -> tuple[str, float]:
        if not isRecord(entry) or not isinstance(entry.get("value"), str):
            raise RuntimeError(f"{_LABEL} returned invalid probabilities for {id}")
        return (
            entry["value"],
            requiredNumber(_LABEL, entry.get("probability"), f"probability for {id}.{entry['value']}"),
        )

    return dict(entryOf(entry) for entry in value)


def _parseAnswer(id: str, question: ClassifierQuestionValue, answer: dict[str, Any]) -> ClassifierAnswerValue:
    if answer.get("type") == "refusal":
        raise RuntimeError(f"{_LABEL} refused to answer {id}")
    if question.type == "choice":
        if answer.get("type") != "choice" or not isinstance(answer.get("choice"), str):
            raise RuntimeError(f"{_LABEL} did not return a choice answer for {id}")
        return ClassifierChoiceAnswer(
            type="choice",
            choice=answer["choice"],
            probabilities=_choiceProbabilities(answer.get("probabilities"), id),
            confidence=requiredNumber(_LABEL, answer.get("confidence"), f"confidence for {id}"),
        )
    if question.type == "score":
        if answer.get("type") != "score":
            raise RuntimeError(f"{_LABEL} did not return a score answer for {id}")
        return ClassifierScoreAnswer(
            type="score",
            score=requiredNumber(_LABEL, answer.get("score"), f"score for {id}"),
            confidence=requiredNumber(_LABEL, answer.get("confidence"), f"confidence for {id}"),
        )
    if answer.get("type") != "predicate":
        raise RuntimeError(f"{_LABEL} did not return a predicate answer for {id}")
    return ClassifierBoolAnswer(
        type="bool", probability=requiredNumber(_LABEL, answer.get("probability"), f"probability for {id}")
    )


def _parseAnswers(value: Any, context: ClassifierContext) -> dict[str, ClassifierAnswerValue]:
    if not isinstance(value, list):
        raise RuntimeError(f"{_LABEL} returned an unexpected response")  # noqa: TRY004 - upstream throws Error
    byName: dict[str, dict[str, Any]] = {}
    for answer in value:
        if isRecord(answer) and isinstance(answer.get("name"), str):
            byName[answer["name"]] = answer

    def entryOf(id: str, question: ClassifierQuestionValue) -> tuple[str, ClassifierAnswerValue]:
        answer = byName.get(id)
        if not answer:
            raise RuntimeError(f"{_LABEL} did not return an answer for {id}")
        return (id, _parseAnswer(id, question, answer))

    return dict(entryOf(id, question) for id, question in objectEntries(context.questions))


# Cloudflare in front of api.openai.com answers 504 with an HTML page when a request runs longer
# than about five seconds. Large inputs, currently above roughly 600K tokens, hit this limit, and
# retrying the same input runs into it again, so 504 is not retried.
_NO_RETRY_STATUSES = [504]


def _errorMessage(error: Any) -> str:
    if getattr(error, "status", None) == 504:
        return (
            f"{_LABEL} error (504): the request timed out at the gateway. Very large inputs (above roughly 600K"
            " tokens) currently exceed its time limit."
        )
    return formatProviderError(normalizeProviderError(error), f"{_LABEL} error")


async def classify(
    model: ClassifierModel, context: ClassifierContext | Mapping[str, Any], options: Any = None
) -> ClassifierResult:
    """Classification through OpenAI's Decisions API."""
    output = ClassifierResult(
        api=model.api,
        provider=model.provider,
        model=model.id,
        answers={},
        stopReason="stop",
        timestamp=time.time_ns() // 1_000_000,
    )

    try:
        # MISAKA fork: a plain mapping context is read into ClassifierContext.
        context = context if isinstance(context, ClassifierContext) else ClassifierContext.model_validate(context)
        if model.api != "openai-decisions":
            raise RuntimeError(f"Unsupported classifier API: {model.api}")
        body = await postClassifierRequest(
            _LABEL,
            urljoin(f"{model.baseUrl.rstrip('/')}/", "decisions"),
            model,
            {
                "model": model.id,
                "input": _wireInput(context),
                "questions": [_wireQuestion(id, question) for id, question in objectEntries(context.questions)],
            },
            options,
            _NO_RETRY_STATUSES,
        )
        if not isRecord(body):
            raise RuntimeError(f"{_LABEL} returned an unexpected response")
        # Set before parsing answers: a request with malformed or refused answers was still billed.
        usage = parseClassifierUsage(body.get("usage"), model)
        if usage is not None:
            output.usage = usage
        output.answers = _parseAnswers(body.get("answers"), context)
        return output
    except Exception as error:  # noqa: BLE001 - the failure is the result
        output.answers = {}
        output.stopReason = "aborted" if signal_aborted(_option(options, "signal")) else "error"
        output.errorMessage = _errorMessage(error)
        return output


__all__ = ["classify"]

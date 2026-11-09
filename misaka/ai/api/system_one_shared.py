"""Translated from pi's ``packages/ai/src/api/system-one-shared.ts`` (v1.1.0).

MISAKA fork (each is marked where it happens): a plain mapping context is read into ``ClassifierContext``, the
transport interface is a dataclass of callables, and a question model is spread through ``model_dump``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypedDict

from misaka.ai.api._common import _option
from misaka.ai.api.classifier_shared import (
    isRecord,
    parseClassifierUsage,
    postClassifierRequest,
    requiredNumber,
)
from misaka.ai.types import (
    ClassifierAnswerValue,
    ClassifierApi,
    ClassifierBoolAnswer,
    ClassifierChoiceAnswer,
    ClassifierContext,
    ClassifierModel,
    ClassifierResult,
    ClassifierScoreAnswer,
)
from misaka.ai.utils.error_body import formatProviderError, normalizeProviderError
from misaka.utils.js import objectEntries
from misaka.utils.values import signal_aborted


class SystemOneWireRequest(TypedDict):
    """TypeSafe System One request body without the transport-specific envelope."""

    state: Any
    questions: dict[str, Any]


@dataclass(slots=True)
class SystemOneTransport:
    """Differences between services that serve System One models."""

    # MISAKA fork: upstream's interface, as a dataclass of callables.
    # Classifier API implemented by this transport.
    api: ClassifierApi
    # Service name used in error messages.
    label: str
    # Absolute request URL.
    url: Callable[[ClassifierModel], str]
    # Wraps the System One request in the service's request envelope.
    payload: Callable[[ClassifierModel, SystemOneWireRequest], Any]
    # Extracts the System One output (`{ answers, usage }`) from the service's response envelope.
    output: Callable[[Any], dict[str, Any]]


def _probabilities(label: str, value: Any, id: str) -> dict[str, float]:
    if not isRecord(value):
        raise RuntimeError(f"{label} returned invalid probabilities for {id}")
    return {
        key: requiredNumber(label, probability, f"probability for {id}.{key}") for key, probability in objectEntries(value)
    }


def _parseAnswers(label: str, value: Any, context: ClassifierContext) -> dict[str, ClassifierAnswerValue]:
    if not isRecord(value):
        raise RuntimeError(f"{label} returned an unexpected response")
    answers: list[tuple[str, ClassifierAnswerValue]] = []
    for id, question in objectEntries(context.questions):
        answer = value.get(id)
        if not isRecord(answer):
            raise RuntimeError(f"{label} did not return an answer for {id}")
        if question.type == "choice":
            if answer.get("type") != "choice" or not isinstance(answer.get("choice"), str):
                raise RuntimeError(f"{label} did not return a choice answer for {id}")
            answers.append(
                (
                    id,
                    ClassifierChoiceAnswer(
                        type="choice",
                        choice=answer["choice"],
                        probabilities=_probabilities(label, answer.get("probabilities"), id),
                        confidence=requiredNumber(label, answer.get("confidence"), f"confidence for {id}"),
                    ),
                )
            )
        elif question.type == "score":
            if answer.get("type") != "score":
                raise RuntimeError(f"{label} did not return a score answer for {id}")
            answers.append(
                (
                    id,
                    ClassifierScoreAnswer(
                        type="score",
                        score=requiredNumber(label, answer.get("score"), f"score for {id}"),
                        confidence=requiredNumber(label, answer.get("confidence"), f"confidence for {id}"),
                    ),
                )
            )
        else:
            if answer.get("type") != "noul":
                raise RuntimeError(f"{label} did not return a bool answer for {id}")
            answers.append(
                (
                    id,
                    ClassifierBoolAnswer(
                        type="bool",
                        probability=requiredNumber(label, answer.get("noul"), f"probability for {id}"),
                    ),
                )
            )
    return dict(answers)


def _wireRequest(context: ClassifierContext) -> SystemOneWireRequest:
    """Maps public `bool` questions to TypeSafe's wire-level `noul` type."""

    # MISAKA fork: a question is a pydantic model; `model_dump` stands in for the object spread.
    def wire(question: Any) -> dict[str, Any]:
        return question.model_dump(exclude_none=True)

    return {
        "state": context.state,
        "questions": {
            id: {**wire(question), "type": "noul"} if question.type == "bool" else wire(question)
            for id, question in objectEntries(context.questions)
        },
    }


async def classifySystemOne(
    transport: SystemOneTransport,
    model: ClassifierModel,
    context: ClassifierContext | Mapping[str, Any],
    options: Any,
) -> ClassifierResult:
    """Runs one System One classification over the given transport."""
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
        if model.api != transport.api:
            raise RuntimeError(f"Unsupported classifier API: {model.api}")
        if context.images:
            raise RuntimeError(f"{transport.label} does not support image input")
        body = await postClassifierRequest(
            transport.label,
            transport.url(model),
            model,
            transport.payload(model, _wireRequest(context)),
            options,
        )
        result = transport.output(body)
        # Set before parsing answers: a request with malformed answers was still billed.
        usage = parseClassifierUsage(result.get("usage"), model)
        if usage is not None:
            output.usage = usage
        output.answers = _parseAnswers(transport.label, result.get("answers"), context)
        return output
    except Exception as error:  # noqa: BLE001 - the failure is the result
        output.stopReason = "aborted" if signal_aborted(_option(options, "signal")) else "error"
        output.errorMessage = formatProviderError(normalizeProviderError(error), f"{transport.label} error")
        return output


__all__ = ["SystemOneTransport", "SystemOneWireRequest", "classifySystemOne"]

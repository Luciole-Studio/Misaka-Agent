"""``models.*`` for code mode scripts, copied from the coding agent's code mode: the model catalog, and classifier and
image model calls.

Translated from pi's ``packages/durable/src/codemode/models.ts`` (v1.3.0). The catalog functions are host globals,
journaled like any global. ``models.classify()`` and ``models.generateImages()`` are paid effects, so each runs as an
internal tool (``callers: ["tools"]``, a reserved name, hidden from scripts' tool listings): a nested durable call with
its own task, live row, abort, usage accounting, and replay policy. The code mode tool turns a script's call of the
global into a call of that tool. Models and their results are misaka.ai's pydantic models; scripts get their JSON.
"""

from __future__ import annotations

import re
from typing import Any

from misaka.ai.auth.types import AuthOperationOptions
from misaka.ai.types import ClassifierContext, ImagesContext
from misaka.chord.context import Context
from misaka.utils.js import UNDEFINED, jsonParse, jsonStringify, jsString

from ..harness.define import defineTool

CODEMODE_INTERNAL_PREFIX = "pi.codemode."
"""Reserved names of code mode's internal tools; they are never listed to scripts or the model."""
CLASSIFY_TOOL = f"{CODEMODE_INTERNAL_PREFIX}models.classify"
GENERATE_IMAGES_TOOL = f"{CODEMODE_INTERNAL_PREFIX}models.generateImages"

MODEL_CALLS: dict[str, str] = {
    "models.classify": CLASSIFY_TOOL,
    "models.generateImages": GENERATE_IMAGES_TOOL,
}
"""The global a script calls, by internal tool."""

CATALOG_GLOBALS = ("models.getModelsOfType", "models.getAvailableOfType", "models.getModelOfType")
"""The catalog functions, host globals of the ``models`` namespace."""

MAX_CONCURRENT_MODEL_CALLS = 4
"""``models.classify()`` and ``models.generateImages()`` calls one script may have in flight; the rest wait."""

MODEL_TYPES = ("chat", "image", "classifier")

MODELS_DESCRIPTION = "\n".join(
    [
        "`models` (all functions return promises):",
        '- `models.getModelsOfType(type, provider?)`, `models.getAvailableOfType(type, provider?)` (configured auth only), `models.getModelOfType(type, provider, id)`: the catalog; `type` is "chat", "image", or "classifier".',
        '- `models.classify(model, { state: { ... }, images?: [{ type: "image", data, mimeType }], questions: { <id>: { type: "choice", instructions, criteria: { <label>: <meaning> } } | { type: "score", instructions, criteria: [<lowest level>, ..., <highest level>] } | { type: "bool", instructions, criteria: { true: <meaning>, false: <meaning> } } } })` resolves to `{ answers: { <id>: ... }, stopReason, errorMessage?, usage? }`.',
        '- `models.generateImages(model, { input: [{ type: "text", text: <prompt> }, ...optional { type: "image", data, mimeType }] })` resolves to `{ output: [image blocks], stopReason, errorMessage?, usage? }`; show each image with `image(block)`.',
        f"At most {MAX_CONCURRENT_MODEL_CALLS} classify/generateImages calls run at once; more wait.",
    ]
)


def isRecord(value: Any) -> bool:
    return isinstance(value, dict)


def describeValue(value: Any) -> str:
    """A script value in an error message: ``undefined``, ``a string``, ``an array``, or its keys (``{ prompt }``)."""
    if value is None or value is UNDEFINED:
        return jsString(value)
    if isinstance(value, list):
        return "an empty array" if len(value) == 0 else "an array"
    if isinstance(value, dict):
        keys = list(value)
        if len(keys) == 0:
            return "{}"
        return f"{{ {', '.join(keys[:6])}{', ...' if len(keys) > 6 else ''} }}"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, int | float):
        return "a number"
    return f"a {type(value).__name__}"


def withArticle(word: str) -> str:
    """``an image``, ``a classifier``."""
    return f"{'an' if re.match(r'[aeiou]', word) else 'a'} {word}"


def _shown(value: Any) -> str:
    """``JSON.stringify(value)`` in a message, where ``undefined`` prints as ``undefined``."""
    return "undefined" if value is UNDEFINED else jsonStringify(value)


def toModelType(value: Any) -> str:
    if isinstance(value, str) and value in MODEL_TYPES:
        return value
    raise RuntimeError(f'Unknown model type {_shown(value)}. Use "chat", "image", or "classifier".')


def toProvider(value: Any) -> str | None:
    if value is None or value is UNDEFINED:
        return None
    if not isinstance(value, str):
        raise RuntimeError("provider must be a string")  # noqa: TRY004 - upstream `new Error`
    return value


def toModelInfo(model: Any) -> Any:
    """Catalog entry for scripts. ``headers`` is dropped because configured headers can carry credentials."""
    info = dict(model.model_dump(mode="json", exclude_none=True) if hasattr(model, "model_dump") else model)
    info.pop("headers", None)
    return jsonParse(jsonStringify(info))


def _argument(values: list[Any], index: int) -> Any:
    return values[index] if index < len(values) else UNDEFINED


async def catalogCall(name: str, args: Any, models: Any, context: Context) -> Any:
    """The catalog functions: what a script's call of ``name`` with ``args`` (the argument list) resolves to;
    ``UNDEFINED`` for an unknown model."""
    values = args if isinstance(args, list) else []
    type, provider, id = _argument(values, 0), _argument(values, 1), _argument(values, 2)
    if name == "models.getModelsOfType":
        return [toModelInfo(model) for model in models.getModelsOfType(toModelType(type), toProvider(provider))]
    if name == "models.getAvailableOfType":
        signal = context.abortSignal
        available = await models.getAvailableOfType(
            toModelType(type), toProvider(provider), AuthOperationOptions(**({} if signal is None else {"signal": signal}))
        )
        return [toModelInfo(model) for model in available]
    if not isinstance(provider, str) or not isinstance(id, str):
        raise RuntimeError(  # noqa: TRY004 - upstream `new Error`
            f"models.getModelOfType(type, provider, id) expects three strings, got ({', '.join(describeValue(value) for value in values)}). The provider and the id are separate arguments, for example models.getModelOfType(\"classifier\", \"typesafe\", \"jev-latest\")."
        )
    model = models.getModelOfType(toModelType(type), provider, id)
    return UNDEFINED if model is None else toModelInfo(model)


CLASSIFIER_CONTEXT_SHAPE = '{ state: { ... }, images?: [{ type: "image", data: <base64>, mimeType }], questions: { <id>: { type: "choice", instructions, criteria: { <label>: <meaning> } } | { type: "score", instructions, criteria: [<lowest level>, ..., <highest level>] } | { type: "bool", instructions, criteria: { true: <meaning>, false: <meaning> } } } }'


def checkClassifierContext(context: Any) -> ClassifierContext:
    """Check a script's classifier context, so mistakes fail with the expected shape instead of a provider error."""

    def fail(problem: str) -> RuntimeError:
        return RuntimeError(f"models.classify() {problem}. Expected context: {CLASSIFIER_CONTEXT_SHAPE}.")

    if not isRecord(context):
        raise fail(f"expects a context object as its second argument, got {describeValue(context)}")
    if not isRecord(context.get("state")):
        raise fail(f"context.state must be an object, got {describeValue(context.get('state', UNDEFINED))}")
    images = context.get("images", UNDEFINED)
    if images is not UNDEFINED:
        if not isinstance(images, list):
            raise fail(f"context.images must be an array, got {describeValue(images)}")
        for index, image in enumerate(images):
            if (
                not isRecord(image)
                or image.get("type") != "image"
                or not isinstance(image.get("data"), str)
                or not isinstance(image.get("mimeType"), str)
            ):
                raise fail(f"context.images[{index}] must be an image block, got {describeValue(image)}")
    questions = context.get("questions", UNDEFINED)
    if not isRecord(questions) or len(questions) == 0:
        raise fail(f"context.questions must map question IDs to questions, got {describeValue(questions)}")

    def isStrings(values: list[Any]) -> bool:
        return len(values) > 0 and all(isinstance(value, str) for value in values)

    for id, question in questions.items():
        at = f"context.questions.{id}"
        if not isRecord(question):
            raise fail(f"{at} must be a question object, got {describeValue(question)}")
        if not isinstance(question.get("instructions"), str):
            raise fail(f"{at}.instructions must be a string")
        criteria = question.get("criteria")
        kind = question.get("type", UNDEFINED)
        if kind == "choice":
            if not isRecord(criteria) or not isStrings(list(criteria.values())):
                raise fail(f'{at} is a "choice" question, so criteria must map each label to its meaning')
        elif kind == "score":
            if not isinstance(criteria, list) or not isStrings(criteria):
                raise fail(f'{at} is a "score" question, so criteria must list the levels as strings, lowest first')
        elif kind == "bool":
            if not isRecord(criteria) or not isinstance(criteria.get("true"), str) or not isinstance(criteria.get("false"), str):
                raise fail(f'{at} is a "bool" question, so criteria must be {{ true: string, false: string }}')
        else:
            raise fail(f'{at}.type must be "choice", "score", or "bool", got {_shown(kind)}')
    # MISAKA fork: upstream casts the checked object, so keys it does not know reach the service, which ignores them;
    # misaka.ai's models forbid extra keys, so only the checked ones are kept (as the product's code mode does).
    return ClassifierContext.model_validate(
        {
            "state": context["state"],
            **(
                {"images": [{"type": "image", "data": image["data"], "mimeType": image["mimeType"]} for image in images]}
                if images is not UNDEFINED
                else {}
            ),
            "questions": {
                id: {
                    "type": question["type"],
                    "instructions": question["instructions"],
                    "criteria": (
                        {"true": question["criteria"]["true"], "false": question["criteria"]["false"]}
                        if question["type"] == "bool"
                        else question["criteria"]
                    ),
                }
                for id, question in questions.items()
            },
        }
    )


def checkImagesContext(context: Any) -> ImagesContext:
    """Check a script's image context, so mistakes such as ``{ prompt }`` fail with the expected shape."""

    def fail(problem: str) -> RuntimeError:
        return RuntimeError(
            f'models.generateImages() {problem}. Expected context: {{ input: [{{ type: "text", text: <prompt> }}, ...optional {{ type: "image", data: <base64>, mimeType }} references] }}.'
        )

    if not isRecord(context):
        raise fail(f"expects a context object as its second argument, got {describeValue(context)}")
    input = context.get("input", UNDEFINED)
    if not isinstance(input, list) or len(input) == 0:
        raise fail(f"context.input must be a non-empty array of blocks, got {describeValue(input)}")
    for index, block in enumerate(input):
        if isRecord(block) and block.get("type") == "text" and isinstance(block.get("text"), str):
            continue
        if (
            isRecord(block)
            and block.get("type") == "image"
            and isinstance(block.get("data"), str)
            and isinstance(block.get("mimeType"), str)
        ):
            continue
        raise fail(f"context.input[{index}] must be a text or image block, got {describeValue(block)}")
    # MISAKA fork: only the checked keys are kept, as for the classifier context.
    return ImagesContext.model_validate(
        {
            "input": [
                {"type": "text", "text": block["text"]}
                if block["type"] == "text"
                else {"type": "image", "data": block["data"], "mimeType": block["mimeType"]}
                for block in input
            ]
        }
    )


def resolveModel(name: str, type: str, model: Any, models: Any) -> Any:
    """Resolve the script's model by provider and id only, so a script-supplied baseUrl or headers never receive the
    credentials, with errors that say what to do."""
    listHint = f'List the {type} models you can use with models.getAvailableOfType("{type}").'
    if not isRecord(model) or not isinstance(model.get("provider"), str) or not isinstance(model.get("id"), str):
        undefinedHint = (
            " models.getModelOfType() returns undefined for an unknown provider or id."
            if model is None or model is UNDEFINED
            else ""
        )
        raise RuntimeError(
            f"{name}() expects {withArticle(type)} model as its first argument, got {describeValue(model)}.{undefinedHint} {listHint}"
        )
    provider, id = model["provider"], model["id"]
    resolved = models.getModelOfType(type, provider, id)
    if resolved is not None:
        return resolved
    actualType = next(
        (other for other in MODEL_TYPES if other != type and models.getModelOfType(other, provider, id) is not None),
        None,
    )
    raise RuntimeError(
        f'"{provider}/{id}" is {withArticle(actualType)} model, not {withArticle(type)} model. {listHint}'
        if actualType is not None
        else f'Unknown {type} model "{provider}/{id}". {listHint}'
    )


ModelCallParameters: dict[str, Any] = {"type": "object", "required": ["model", "context"], "properties": {"model": {}, "context": {}}}

_UNKNOWN_SCHEMA: dict[str, Any] = {}
"""``Type.Unknown()``."""


def _dump(value: Any) -> Any:
    """A classifier or image result as the JSON the script receives: fields the service left unset are absent, as
    ``undefined`` properties are (``JSON.parse(JSON.stringify(value))``)."""
    data = value.model_dump(mode="json", exclude_none=True) if hasattr(value, "model_dump") else value
    return jsonParse(jsonStringify(data))


def modelResult(label: str, result: Any, value: Any) -> dict[str, Any]:
    """The result of a model call as the tool returns it: the whole result for the script, usage counted by the
    Harness."""
    ok = result.stopReason == "stop"
    usage = getattr(result, "usage", None)
    return {
        "output": [
            {
                "type": "text",
                "text": f"{label} done" if ok else f"{label} {result.stopReason}: {result.errorMessage or ''}",
            }
        ],
        "structuredOutput": _dump(value),
        **({} if ok else {"isError": True}),
        # Optional counters the provider left unset stay absent, as upstream's `undefined` ones do.
        **({} if usage is None else {"usage": usage.model_dump(mode="json", exclude_none=True)}),
    }


def createModelTools() -> list[dict[str, Any]]:
    """The internal tools behind ``models.classify()`` and ``models.generateImages()``."""

    def signalOptions(context: Context) -> dict[str, Any]:
        return {} if context.abortSignal is None else {"signal": context.abortSignal}

    async def classify(args: dict[str, Any], api: Any, context: Context) -> dict[str, Any]:
        model = resolveModel("models.classify", "classifier", args.get("model"), api.models)
        checked = checkClassifierContext(args.get("context"))
        result = await api.models.classify(model, checked, signalOptions(context))
        return modelResult(f"classify {model.provider}/{model.id}", result, result)

    async def generateImages(args: dict[str, Any], api: Any, context: Context) -> dict[str, Any]:
        model = resolveModel("models.generateImages", "image", args.get("model"), api.models)
        checked = checkImagesContext(args.get("context"))
        result = await api.models.generateImages(model, checked, signalOptions(context))
        return modelResult(f"generateImages {model.provider}/{model.id}", result, result)

    return [
        defineTool(
            {
                "name": CLASSIFY_TOOL,
                "description": "models.classify() of code mode scripts.",
                "parameters": ModelCallParameters,
                "structuredOutputSchema": _UNKNOWN_SCHEMA,
                "callers": ["tools"],
                "execute": classify,
            }
        ),
        defineTool(
            {
                "name": GENERATE_IMAGES_TOOL,
                "description": "models.generateImages() of code mode scripts.",
                "parameters": ModelCallParameters,
                "structuredOutputSchema": _UNKNOWN_SCHEMA,
                "callers": ["tools"],
                "execute": generateImages,
            }
        ),
    ]


__all__ = [
    "CATALOG_GLOBALS",
    "CLASSIFIER_CONTEXT_SHAPE",
    "CLASSIFY_TOOL",
    "CODEMODE_INTERNAL_PREFIX",
    "GENERATE_IMAGES_TOOL",
    "MAX_CONCURRENT_MODEL_CALLS",
    "MODELS_DESCRIPTION",
    "MODEL_CALLS",
    "catalogCall",
    "checkClassifierContext",
    "checkImagesContext",
    "createModelTools",
    "describeValue",
    "resolveModel",
    "toModelInfo",
    "withArticle",
]

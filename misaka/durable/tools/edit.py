"""The ``edit`` tool.

Translated from pi's ``packages/durable/src/tools/edit.ts``. The parameters are the JSON schema TypeBox builds for the
upstream ``Type.Object``; the result details are ``{"diff", "patch", "firstChangedLine"?}``.
"""

from __future__ import annotations

from typing import Any

from misaka.chord.context import Context
from misaka.utils.js import jsonParse

from ..env import FileError
from ..harness.define import defineTool
from .edit_diff import (
    applyEditsToNormalizedContent,
    detectLineEnding,
    generateDiffString,
    generateUnifiedPatch,
    normalizeToLF,
    restoreLineEndings,
    stripBom,
)
from .env import requireEnv
from .file_mutation_queue import withFileMutationQueue
from .path_utils import resolveToolPath

replaceEditSchema: dict[str, Any] = {
    "type": "object",
    "required": ["oldText", "newText"],
    "properties": {
        "oldText": {
            "type": "string",
            "description": "Exact text for one targeted replacement. It must be unique in the original file and must not overlap with any other edits[].oldText in the same call.",
        },
        "newText": {"type": "string", "description": "Replacement text for this targeted edit."},
    },
}

editSchema: dict[str, Any] = {
    "type": "object",
    "required": ["path", "edits"],
    "properties": {
        "path": {"type": "string", "description": "Path to the file to edit (relative or absolute)"},
        "edits": {
            "type": "array",
            "items": replaceEditSchema,
            "description": "One or more targeted replacements. Each edit is matched against the original file, not incrementally. Do not include overlapping or nested edits. If two changes touch the same block or nearby lines, merge them into one edit instead.",
        },
    },
}


def isSingleEditInput(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    return isinstance(value.get("oldText"), str) and isinstance(value.get("newText"), str)


def prepareEditArguments(input: Any) -> Any:
    """Repair shapes models commonly send: ``edits`` as a JSON string or as a single edit object, and a top-level
    ``oldText``/``newText`` pair. Works on a copy; the call's arguments stay unchanged."""
    if not isinstance(input, dict):
        return input
    args: dict[str, Any] = {**input}
    if isinstance(args.get("edits"), str):
        try:
            parsed = jsonParse(args["edits"])
            if isinstance(parsed, list):
                args["edits"] = parsed
            elif isSingleEditInput(parsed):
                args["edits"] = [parsed]
        except ValueError:
            pass
    elif isSingleEditInput(args.get("edits")):
        args["edits"] = [args["edits"]]

    if not isinstance(args.get("oldText"), str) or not isinstance(args.get("newText"), str):
        return args
    edits = [*args["edits"]] if isinstance(args.get("edits"), list) else []
    edits.append({"oldText": args["oldText"], "newText": args["newText"]})
    rest = {key: value for key, value in args.items() if key not in ("oldText", "newText")}
    return {**rest, "edits": edits}


def validateEditInput(input: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(input.get("edits"), list) or len(input["edits"]) == 0:
        raise RuntimeError("Edit tool input is invalid. edits must contain at least one replacement.")
    return {"path": input["path"], "edits": input["edits"]}


def editAccessError(path: str, error: FileError) -> Exception:
    failure = RuntimeError(f"Could not edit file: {path}. Error code: {error.code}.")
    failure.__cause__ = error
    failure.cause = error  # type: ignore[attr-defined]
    return failure


def createEditTool() -> dict[str, Any]:
    async def execute(args: dict[str, Any], api: Any, context: Context) -> dict[str, Any]:
        validated = validateEditInput(args)
        path = validated["path"]
        edits = validated["edits"]
        env = requireEnv(api)
        absolutePath = await resolveToolPath(env, path, context)

        async def edit() -> dict[str, Any]:
            signal = context.abortSignal

            def assertLive() -> None:
                if signal is not None and signal.aborted:
                    raise RuntimeError("Operation aborted")

            assertLive()
            info = await env.fileInfo(absolutePath, context)
            if not info["ok"]:
                raise editAccessError(path, info["error"])
            if info["value"]["kind"] != "file" and info["value"]["kind"] != "symlink":
                raise RuntimeError(f"Could not edit file: {path}. Path is not a file.")

            readResult = await env.readTextFile(absolutePath, context)
            if not readResult["ok"]:
                raise editAccessError(path, readResult["error"])
            assertLive()

            stripped = stripBom(readResult["value"])
            bom = stripped["bom"]
            content = stripped["text"]
            originalEnding = detectLineEnding(content)
            normalizedContent = normalizeToLF(content)
            applied = applyEditsToNormalizedContent(normalizedContent, edits, path)
            baseContent = applied["baseContent"]
            newContent = applied["newContent"]
            assertLive()

            finalContent = bom + restoreLineEndings(newContent, originalEnding)
            writeResult = await env.writeFile(absolutePath, finalContent, context)
            if not writeResult["ok"]:
                raise editAccessError(path, writeResult["error"])
            assertLive()

            diffResult = generateDiffString(baseContent, newContent)
            details = {
                "diff": diffResult["diff"],
                "patch": generateUnifiedPatch(path, baseContent, newContent),
                **({} if diffResult["firstChangedLine"] is None else {"firstChangedLine": diffResult["firstChangedLine"]}),
            }
            return {
                "output": [{"type": "text", "text": f"Successfully replaced {len(edits)} block(s) in {path}."}],
                "details": details,
            }

        return await withFileMutationQueue(env, absolutePath, edit, context)

    return defineTool(
        {
            "name": "edit",
            "description": "Edit a single file using exact text replacement. Every edits[].oldText must match a unique, non-overlapping region of the original file. If two changes affect the same block or nearby lines, merge them into one edit instead of emitting overlapping edits. Do not include large unchanged regions just to connect distant changes.",
            "parameters": editSchema,
            "prepareArguments": prepareEditArguments,
            "execute": execute,
        }
    )


__all__ = ["createEditTool"]
